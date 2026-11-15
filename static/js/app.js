
document.addEventListener("DOMContentLoaded",()=>{
 const start=document.querySelector('input[name="start_at"]');
 const end=document.querySelector('input[name="end_at"]');
 if(start&&end){
   const now=new Date();now.setMinutes(now.getMinutes()-now.getTimezoneOffset());
   const min=now.toISOString().slice(0,16);start.min=min;end.min=min;
   start.addEventListener("change",()=>end.min=start.value||min);
 }
});
